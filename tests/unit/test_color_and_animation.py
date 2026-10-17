"""Colour grading / video filters and caption animations (models, FFmpeg filters, ASS events)."""

import re

import pytest

from ai_video_studio.captions.animation import (
    CaptionAnimation,
    AnimationError,
    ENTRANCES,
    EXITS,
    LOOPS,
    WORD_EFFECTS,
    graphemes,
    phases,
    state_at,
)
from ai_video_studio.captions.ass import ass_time, build_ass
from ai_video_studio.captions.model import CaptionSegment, CaptionTrack, Word, output_captions
from ai_video_studio.captions.style import BUILTIN_PRESETS, CaptionStyle, wrap_text
from ai_video_studio.render.batch import RenderPreset
from ai_video_studio.render.color import FILTERS, GRADE_PRESETS, ColorError, ColorPresetStore, ColorSettings
from ai_video_studio.render.pipeline import build_filter_graph
from ai_video_studio.storage.database import Database
from ai_video_studio.storage.settings import SettingsStore


# -- colour --------------------------------------------------------------------------------

def test_every_look_and_filter_maps_to_real_filters():
    for key in GRADE_PRESETS:
        c = ColorSettings(grading=True)
        c.load_preset(key)
        assert bool(c.chain()) == (key not in ("original", "custom")), key
    for key in FILTERS:
        c = ColorSettings(filtering=True, filter=key, intensity=100)
        assert bool(c.chain()) == (key != "none"), key
    assert ColorSettings(filtering=True, filter="black_white").chain() == ["eq=saturation=0.0000"]
    assert ColorSettings(filtering=True, filter="sepia").chain()[0].startswith("colorchannelmixer=rr=0.3930")


def test_intensity_really_scales_the_filter_and_zero_keeps_the_original():
    def eq(c):
        return dict(kv.split("=") for kv in next(f for f in c.chain() if f.startswith("eq="))[3:].split(":"))

    assert ColorSettings(filtering=True, filter="vibrant", intensity=0).chain() == []
    assert not ColorSettings(filtering=True, filter="vibrant", intensity=0).active
    full = float(eq(ColorSettings(filtering=True, filter="vibrant", intensity=100))["saturation"])
    half = float(eq(ColorSettings(filtering=True, filter="vibrant", intensity=50))["saturation"])
    assert full == pytest.approx(1.45) and half == pytest.approx(1.225)
    sepia50 = ColorSettings(filtering=True, filter="sepia", intensity=50).chain()[0]
    assert "rr=0.6965" in sepia50  # halfway between the identity (1) and sepia (0.393)


def test_grade_and_filter_are_combined_once_and_clamped():
    c = ColorSettings(grading=True, filtering=True, filter="high_contrast", intensity=100)
    c.load_preset("cinematic")
    combined = c.combined()
    assert combined["contrast"] == 18 + 45 and combined["vignette"] == 15
    assert sum(f.startswith("eq=") for f in c.chain()) == 1  # one eq, not one per source
    strong = ColorSettings(grading=True, values={"saturation": 100}, filtering=True, filter="vibrant")
    assert strong.combined()["saturation"] == 100  # clamped to the slider range
    off = ColorSettings(grading=False, values={"contrast": 50}, filtering=False, filter="vibrant")
    assert not off.active and off.chain() == []  # switched off: nothing is applied


def test_invalid_colour_values_are_rejected():
    for bad in ({"values": {"contrast": 500}}, {"values": {"bogus": 1}}, {"intensity": 101}, {"filter": "x"},
                {"preset": "nope"}, {"values": {"exposure": float("nan")}}):
        with pytest.raises(ColorError):
            ColorSettings(**bad).validate()
    assert ColorSettings.from_dict({"values": {"contrast": "huge"}}) == ColorSettings()  # damaged: no change
    with pytest.raises(ColorError):
        ColorSettings.from_dict({"intensity": 300}, strict=True)


def test_color_graph_runs_after_framing_and_before_captions():
    c = ColorSettings(grading=True, values={"contrast": 20})
    graph, _v, _a = build_filter_graph(keep_ranges=None, has_audio=False, video_stage=lambda a, b: f"[{a}]scale=8:8[{b}]",
                                       subtitle_file="captions.ass", color_stage=c.graph())
    assert graph.index("scale=8:8") < graph.index("eq=contrast") < graph.index("ass=captions.ass:shaping=complex")


def test_custom_color_presets_are_stored_validated_and_builtins_stay(tmp_path):
    store = ColorPresetStore(SettingsStore(Database(tmp_path / "s.db")))
    assert store.names() == []
    store.save("My Look", {"contrast": 12.0, "temperature": -20.0})
    assert store.names() == ["My Look"] and store.get("My Look") == {"contrast": 12.0, "temperature": -20.0}
    store.save("My Look", {"contrast": 30.0})  # update
    assert store.get("My Look") == {"contrast": 30.0}
    with pytest.raises(ColorError):
        store.save("Cinematic", {"contrast": 1.0})  # a built-in name
    with pytest.raises(ColorError):
        store.save("Broken", {"contrast": 999.0})  # only validated values are kept
    store.delete("My Look")
    assert store.names() == [] and "cinematic" in GRADE_PRESETS


def test_render_preset_keeps_color_and_animation():
    p = RenderPreset(color=ColorSettings(grading=True, preset="teal_orange", values={"split_tone": 60.0}),
                     caption_animation=CaptionAnimation(entrance="pop").to_dict())
    back = RenderPreset.from_dict(p.to_dict())
    assert back.color.values == {"split_tone": 60.0} and back.caption_animation["entrance"] == "pop"
    assert RenderPreset.from_dict({"caption_animation": {"entrance": "nope"}}).caption_animation is None


# -- Bangla text ---------------------------------------------------------------------------

def test_graphemes_keep_bengali_conjuncts_vowel_signs_and_phala_whole():
    assert graphemes("ক্ষমা") == ["ক্ষ", "মা"]
    assert graphemes("স্কুল") == ["স্কু", "ল"]
    assert graphemes("রবীন্দ্রনাথ") == ["র", "বী", "ন্দ্র", "না", "থ"]
    assert graphemes("কোথায়") == ["কো", "থা", "য়"]
    assert graphemes("ব্যাংক") == ["ব্যাং", "ক"]  # য-ফলা and the anusvara (a spacing mark) stay attached
    assert graphemes("Hi, বাংলা!") == ["H", "i", ",", " ", "বাং", "লা", "!"]
    assert graphemes("👍🏽") == ["👍🏽"]
    for text in ("যুক্তাক্ষর English মিশ্র।", "ক্ষমা স্কুল"):
        assert "".join(graphemes(text)) == text  # nothing lost or reordered


def test_wrapping_never_splits_a_cluster_and_counts_clusters():
    text = "রবীন্দ্রনাথঠাকুরেরগানগুলো"  # one long word without spaces
    lines = wrap_text(text, 5)
    assert "".join(lines) == text
    for line in lines:
        assert graphemes(line) == graphemes(line)  # each piece is whole clusters
        assert not line.startswith("্") and "্" != line[-1]  # no line starts/ends inside a conjunct
    assert wrap_text("ক্ষমা ক্ষমা", 5) == ["ক্ষমা ক্ষমা"]  # 2 + 1 + 2 clusters fit in 5
    assert wrap_text("hello world", 5) == ["hello", "world"]  # English unchanged


# -- animations ------------------------------------------------------------------------------

def events(ass: str) -> list[tuple[float, float, str, str]]:
    out = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            h, m, s = parts[1].split(":")
            h2, m2, s2 = parts[2].split(":")
            out.append((int(h) * 3600 + int(m) * 60 + float(s), int(h2) * 3600 + int(m2) * 60 + float(s2),
                        parts[3], parts[9]))
    return out


def test_static_captions_are_unchanged_without_animation():
    segs = [CaptionSegment(0.5, 2.0, "Hello"), CaptionSegment(2.5, 4.0, "বাংলা")]
    style = CaptionStyle(fade_in_ms=100)
    plain = build_ass(segs, style, 1280, 720)
    assert plain == build_ass(segs, style, 1280, 720, animation=CaptionAnimation())  # "none" = static
    assert [e[3] for e in events(plain)] == [r"{\fad(100,0)}Hello", r"{\fad(100,0)}বাংলা"]


def test_every_preset_renders_inside_its_caption_without_gaps():
    seg = CaptionSegment(1.0, 4.0, "Hello there world", words=[])
    for entrance in ENTRANCES:
        for leave in EXITS:
            for loop in LOOPS:
                anim = CaptionAnimation(entrance=entrance, exit=leave, loop=loop)
                ass = build_ass([seg], CaptionStyle(background=True), 640, 360, animation=anim)
                text_events = [e for e in events(ass) if e[2] == "Default"]
                assert text_events[0][0] == pytest.approx(1.0) and text_events[-1][1] == pytest.approx(4.0)
                for a, b in zip(text_events, text_events[1:]):
                    assert b[0] == pytest.approx(a[1])  # back to back: no flicker
                assert len([e for e in events(ass) if e[2] == "Box"]) == len(text_events)  # the box follows
    for word in WORD_EFFECTS:
        CaptionAnimation(word=word).validate()


def test_entrance_and_exit_tags():
    seg = CaptionSegment(1.0, 3.0, "Hi")
    ass = build_ass([seg], CaptionStyle(), 1000, 1000, animation=CaptionAnimation(entrance="slide_left", exit="fade",
                                                                                 in_ms=500, out_ms=400))
    first, middle, last = events(ass)
    assert (first[0], first[1]) == (1.0, 1.5) and r"\move(380.0,920.0,500.0,920.0,0,500)" in first[3]
    assert r"\1a&HFF&" in first[3] and r"\1a&H00&" in first[3].split(r"\t(")[1]  # fades in while sliding
    assert (middle[0], middle[1]) == (1.5, 2.6) and r"\pos(500.0,920.0)" in middle[3]
    assert (last[0], last[1]) == (2.6, 3.0) and r"\t(0,400,\1a&HFF&" in last[3]
    pop = events(build_ass([seg], CaptionStyle(), 1000, 1000, animation=CaptionAnimation(entrance="pop")))
    assert r"\fscx0.0\fscy0.0" in pop[0][3] and r"\fscx115.0" in pop[0][3] and r"\fscx100.0" in pop[1][3]


def test_short_captions_and_delay_stay_inside_the_caption():
    anim = CaptionAnimation(entrance="pop", exit="fade", in_ms=800, out_ms=800, delay_ms=500)
    p = phases(anim, 0.0, 0.5)
    assert p.show == 0.25 and p.body <= p.exit <= p.end == 0.5  # delay ≤ half, in/out ≤ 45 % each
    tiny = phases(anim, 0.0, 0.06)
    assert tiny.body == tiny.show and tiny.exit == tiny.end  # too short to animate: shown plainly
    ass = build_ass([CaptionSegment(10.0, 10.06, "ok")], CaptionStyle(), 640, 360, animation=anim)
    assert all(10.0 <= e[0] < e[1] <= 10.06 + 1e-9 for e in events(ass))
    assert state_at(anim, 0.0, 2.0, 0.1) is None and state_at(anim, 0.0, 2.0, 1.3)["s"] == 100


def test_typewriter_reveals_whole_bengali_clusters():
    seg = CaptionSegment(0.0, 2.0, "ক্ষমা হও")
    ass = build_ass([seg], CaptionStyle(), 640, 360, animation=CaptionAnimation(word="typewriter"))
    text = events(ass)[0][3]
    pieces = [p for p in re.split(r"\{[^}]*\}", text) if p]
    assert pieces == ["ক্ষ", "মা ", "হ", "ও"]  # never inside a cluster (the space follows its word)
    assert text.count(r"\1a&HFF&") == 3  # every cluster after the first starts hidden


def test_word_effects_follow_whisper_word_times_or_spread_evenly():
    words = [Word(1.0, 1.4, " one"), Word(2.0, 2.3, " two"), Word(2.6, 2.9, " three")]
    seg = CaptionSegment(1.0, 3.0, "one two three", words=words)
    karaoke = events(build_ass([seg], CaptionStyle(highlight_color="#FF0000"), 640, 360,
                               animation=CaptionAnimation(word="karaoke")))[0][3]
    assert r"\1c&H0000FF&\2c&HFFFFFF&" in karaoke  # filled = highlight colour, before = text colour
    # each word fills over its spoken time, then waits for the next one (Whisper times)
    assert r"{\kf40}one {\k60}{\kf30}two {\k30}{\kf30}three" in karaoke
    highlight = events(build_ass([seg], CaptionStyle(), 640, 360, animation=CaptionAnimation(word="highlight")))
    assert r"\t(1000,1001,\1c&H00E6FF&)" in highlight[0][3]  # "two" lights up at 2.0 s (1000 ms in)
    no_times = CaptionSegment(0.0, 2.0, "aa bbbb")  # fallback: shares by length up to 85 % of the caption
    reveal = events(build_ass([no_times], CaptionStyle(), 640, 360, animation=CaptionAnimation(word="word_reveal")))
    assert r"\t(567,717," in reveal[0][3]  # "bbbb" starts at 1.7 * 2/6 s


def test_conflicting_animation_choices_are_rejected():
    with pytest.raises(AnimationError):
        CaptionAnimation(entrance="typewriter", word="karaoke").validate()
    with pytest.raises(AnimationError):
        CaptionAnimation(word="word_pop", loop="pulse").validate()
    with pytest.raises(AnimationError):
        CaptionAnimation(in_ms=-1).validate()


def test_animation_survives_cuts_speed_split_and_project_files():
    anim = CaptionAnimation(entrance="pop").to_dict()
    track = CaptionTrack([CaptionSegment(0.0, 2.0, "one two", animation=anim)])
    a, b = track.split(track.segments[0].id, text_index=3)
    assert a.animation == anim and b.animation == anim
    out = output_captions(track.segments, [(0.0, 1.5)], 2.0)
    assert all(s.animation == anim for s in out)
    assert CaptionTrack.from_dict(track.to_dict()).segments[0].animation == anim
    assert "animation" not in CaptionSegment(0, 1, "x").to_dict()  # static captions: unchanged format


def test_new_appearance_templates_are_valid():
    for name in ("Clean Pop", "Cinematic Subtitle", "Bold Social Media", "Minimal Subtitle", "Karaoke Highlight",
                 "Dynamic Caption", "Smooth Word Reveal"):
        BUILTIN_PRESETS[name].validate()
    assert ass_time(1.5) == "0:00:01.50"
