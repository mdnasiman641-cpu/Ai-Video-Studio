"""Colour grading, video filters and caption animations in real exports (FFmpeg + libass).

The exported pixels are measured: black & white really is grey, Teal & Orange really moves the
shadows towards teal and the highlights towards orange, filter intensity really scales the
change, and animated captions (Pop In, Fade Out, Karaoke) appear, grow and fade at the right
times, with Bengali text, inside each caption's time.
"""

import subprocess

import pytest

from ai_video_studio.captions.animation import CaptionAnimation
from ai_video_studio.captions.model import CaptionSegment, Word
from ai_video_studio.captions.style import CaptionStyle
from ai_video_studio.core.ffmpeg_tools import find_tool, probe_video
from ai_video_studio.jobs.manager import JobManager
from ai_video_studio.jobs.models import JobState
from ai_video_studio.jobs.render_jobs import (
    RENDER_CLIP_JOB,
    RENDER_JOB,
    make_render_clip_handler,
    make_render_handler,
)
from ai_video_studio.jobs.store import JobStore
from ai_video_studio.render.color import ColorSettings
from ai_video_studio.render.layout import ResizeSettings, compute_layout
from ai_video_studio.render.output import OutputSettings
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg
W, H = 320, 180


def ffmpeg():
    return find_tool("ffmpeg")


@pytest.fixture
def manager(db):
    m = JobManager(JobStore(db), 2)
    m.register(RENDER_JOB, make_render_handler(ffmpeg))
    m.register(RENDER_CLIP_JOB, make_render_clip_handler(ffmpeg))
    yield m
    m.shutdown()


def run(manager, kind, payload, timeout=120):
    job = manager.submit(kind, kind, payload)
    assert manager.wait_idle(timeout)
    done = manager.store.get(job.id)
    assert done.state is JobState.COMPLETED, done.error
    return done


@pytest.fixture(scope="module")
def ramp(tmp_path_factory):
    """A grey ramp (black on the left, white on the right) with a tone, 3 s."""
    path = tmp_path_factory.mktemp("color") / "ramp.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=25:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-vf", f"geq=lum='X*255/{W}':cb=128:cr=128", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def payload(src, out, **extra):
    return {"path": str(src), "media_id": None, "output": str(out), "duration": 3.0, "fps": 25.0, "has_audio": True,
            "audio_codec": "aac", "keep_ranges": None,
            "output_settings": OutputSettings(encoder="cpu", quality="high").to_dict()} | extra


def rgb(path, at, size=(W, H)):
    raw = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    w, h = size
    return [[tuple(raw[(y * w + x) * 3:(y * w + x) * 3 + 3]) for x in range(w)] for y in range(h)]


def column_mean(frame, x):
    rows = [frame[y][x] for y in range(len(frame))]
    return tuple(sum(p[c] for p in rows) / len(rows) for c in range(3))


def test_black_and_white_with_resize_is_grey_and_keeps_the_size(manager, ramp, tmp_path):
    out = tmp_path / "bw.mp4"
    resize = ResizeSettings(preset="custom", width=240, height=240, mode="blur")
    stage = {"type": "layout", "layout": compute_layout(W, H, resize).to_dict()}
    color = ColorSettings(filtering=True, filter="black_white", intensity=100)
    run(manager, RENDER_JOB, payload(ramp, out, stage=stage, color=color.to_dict() | {"height": 240}))
    meta = probe_video(out, find_tool("ffprobe"))
    assert (meta.width, meta.height) == (240, 240) and meta.has_audio
    frame = rgb(out, 1.0, (240, 240))
    assert max(max(p) - min(p) for row in frame for p in row) <= 6  # every pixel grey


def test_teal_orange_moves_shadows_to_teal_and_highlights_to_orange(manager, ramp, tmp_path):
    plain, graded = tmp_path / "plain.mp4", tmp_path / "teal.mp4"
    run(manager, RENDER_JOB, payload(ramp, plain))
    grade = ColorSettings(grading=True)
    grade.load_preset("teal_orange")
    run(manager, RENDER_JOB, payload(ramp, graded, color=grade.to_dict()))
    before, after = rgb(plain, 1.0), rgb(graded, 1.0)
    dark, bright = round(W * 0.25), round(W * 0.8)
    r, g, b = column_mean(after, dark)
    assert b > r + 6 and g > r  # shadows: teal
    r, g, b = column_mean(after, bright)
    assert r > b + 6  # highlights: orange
    r0, _g0, b0 = column_mean(before, bright)
    assert abs(r0 - b0) < 4  # the source is neutral grey


def test_filter_intensity_controls_the_strength(manager, ramp, tmp_path):
    means = {}
    for level in (0, 50, 100):
        out = tmp_path / f"sepia{level}.mp4"
        color = ColorSettings(filtering=True, filter="sepia", intensity=level)
        run(manager, RENDER_JOB, payload(ramp, out, color=color.to_dict() if color.active else None))
        r, _g, b = column_mean(rgb(out, 1.0), round(W * 0.6))
        means[level] = r - b  # sepia: warmer
    assert means[0] < 4 < means[50] < means[100]
    assert means[100] > 2 * means[50] * 0.75  # roughly proportional


def brightness(frame) -> int:
    return sum(r + g + b for row in frame for (r, g, b) in row)


def yellow(frame) -> int:
    return sum(1 for row in frame for (r, g, b) in row if r > 180 and g > 160 and b < 90)


@pytest.fixture(scope="module")
def black(tmp_path_factory):
    path = tmp_path_factory.mktemp("anim") / "black.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def caption_payload(src, out, segments, animation, style=None, **extra):
    style = style or CaptionStyle(font_size=90, text_color="#FFE600", outline_width=0, shadow_offset=0,
                                  vertical="middle")
    return payload(src, out, duration=4.0, captions={"segments": [s.to_dict() for s in segments],
                                                     "style": style.to_dict(), "width": 640, "height": 360,
                                                     "animation": animation.to_dict()}, **extra)


def test_pop_in_and_fade_out_appear_in_the_export_with_bengali_text(manager, black, tmp_path):
    out = tmp_path / "anim.mp4"
    segments = [CaptionSegment(0.5, 3.0, "HELLO বাংলা ক্ষমা"), CaptionSegment(3.3, 3.42, "short")]
    anim = CaptionAnimation(entrance="pop", exit="fade", in_ms=600, out_ms=600)
    run(manager, RENDER_JOB, caption_payload(black, out, segments, anim))
    size = (640, 360)
    early, mid, fading, gone = (brightness(rgb(out, t, size)) for t in (0.62, 1.6, 2.86, 3.15))
    assert yellow(rgb(out, 1.6, size)) > 500  # the caption is fully shown, in its colour, in the middle
    assert 0 < early < mid * 0.5  # popping in: still small and transparent
    assert 0 < fading < mid * 0.6  # fading out: dimmer
    assert gone < mid * 0.01  # after the caption
    assert brightness(rgb(out, 0.3, size)) < mid * 0.01  # before the caption


def test_karaoke_follows_word_times(manager, black, tmp_path):
    out = tmp_path / "karaoke.mp4"
    words = [Word(0.5, 1.0, " AAAA"), Word(1.5, 2.0, " BBBB"), Word(2.5, 3.0, " CCCC")]
    seg = CaptionSegment(0.5, 3.5, "AAAA BBBB CCCC", words=words)
    style = CaptionStyle(font_size=90, text_color="#3060FF", highlight_color="#FFE600", outline_width=0,
                         shadow_offset=0, vertical="middle")
    run(manager, RENDER_JOB, caption_payload(black, out, [seg], CaptionAnimation(word="karaoke"), style))
    size = (640, 360)
    counts = [yellow(rgb(out, t, size)) for t in (0.55, 1.2, 2.2, 3.2)]
    assert counts[0] < counts[1] < counts[2] < counts[3]  # the highlight grows word by word
    assert counts[1] == pytest.approx(counts[3] / 3, rel=0.35)  # one of three words filled at 1.2 s


def test_animation_preview_clip_is_rendered_to_the_temp_folder(manager, black, tmp_path):
    from ai_video_studio.captions.ass import build_ass

    ass = build_ass([CaptionSegment(0.4, 1.4, "Preview")], CaptionStyle(), 320, 180,
                    animation=CaptionAnimation(entrance="slide_up"))
    job = run(manager, RENDER_CLIP_JOB, {"path": str(black), "start": 1.0, "end": 2.8, "ass": ass, "height": 180})
    clip = job.result["clip"]
    meta = probe_video(clip, find_tool("ffprobe"))
    assert meta.height == 180 and abs(meta.duration - 1.8) < 0.15
    assert "previews" in clip and str(black.parent) not in clip  # a temp preview, never next to the source
