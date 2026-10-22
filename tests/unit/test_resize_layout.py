"""Resize & Crop geometry and filter generation (the same numbers drive preview and export)."""

import pytest

from ai_video_studio.render.layout import (
    PRESETS,
    LayoutError,
    ResizeSettings,
    aspect_text,
    compute_layout,
    frame_filter,
    layout_filter,
    layout_notes,
)


def lay(src, **kw):
    return compute_layout(src[0], src[1], ResizeSettings(**kw))


def all_even(layout):
    return all(v % 2 == 0 for v in (layout.out_w, layout.out_h, layout.src_x, layout.src_y, layout.src_w,
                                     layout.src_h, layout.dst_x, layout.dst_y, layout.dst_w, layout.dst_h))


def inside(layout, src):
    return (layout.src_x + layout.src_w <= src[0] and layout.src_y + layout.src_h <= src[1]
            and layout.dst_x + layout.dst_w <= layout.out_w and layout.dst_y + layout.dst_h <= layout.out_h)


@pytest.mark.parametrize("preset", [p for p in PRESETS if p not in ("custom",)])
@pytest.mark.parametrize("mode", ["fit", "fill", "blur", "crop", "custom"])
@pytest.mark.parametrize("src", [(1920, 1080), (1080, 1920), (1279, 719), (640, 480)])
def test_every_preset_and_mode_gives_valid_even_geometry(preset, mode, src):
    layout = lay(src, preset=preset, mode=mode, crop_x=0.1, crop_y=0.1, crop_w=0.5, crop_h=0.6, zoom=1.3,
                 pos_x=0.4, pos_y=0.6)
    size = PRESETS[preset][1] or (src[0] // 2 * 2, src[1] // 2 * 2)
    assert (layout.out_w, layout.out_h) == size
    assert all_even(layout) and inside(layout, src)
    assert layout.src_w >= 2 and layout.dst_w >= 2


def test_fit_landscape_into_vertical_pads_top_and_bottom():
    layout = lay((1920, 1080), preset="1080x1920", mode="fit")
    assert (layout.src_w, layout.src_h) == (1920, 1080)
    assert (layout.dst_x, layout.dst_w, layout.dst_h) == (0, 1080, 608)
    assert layout.dst_y == 656 and layout.background == "color"
    assert aspect_text(1080, 1920) == "9:16" and layout.scale == pytest.approx(0.5625)


def test_fill_crops_and_follows_position():
    centre = lay((1920, 1080), preset="1080x1920", mode="fill")
    assert (centre.dst_x, centre.dst_y, centre.dst_w, centre.dst_h) == (0, 0, 1080, 1920)
    assert centre.src_h == 1080 and centre.src_w == 608 and centre.src_x == 656 and centre.background == "none"
    left = lay((1920, 1080), preset="1080x1920", mode="fill", pos_x=0.0)
    right = lay((1920, 1080), preset="1080x1920", mode="fill", pos_x=1.0)
    assert left.src_x == 0 and right.src_x + right.src_w == 1920
    assert centre.upscaled  # 608 source pixels fill 1080 output pixels
    assert "enlarged" in layout_notes(centre, 1920, 1080)[0]


def test_same_aspect_fit_covers_canvas_without_background():
    layout = lay((3840, 2160), preset="1920x1080", mode="fit")
    assert layout.background == "none" and not layout.upscaled and layout.scale == 0.5


def test_crop_region_with_and_without_aspect_lock():
    locked = lay((1920, 1080), preset="1080x1080", mode="crop", crop_x=0.25, crop_y=0.0, crop_w=0.5, crop_h=1.0)
    assert locked.src_w == locked.src_h == 960 and locked.background == "none"
    free = lay((1920, 1080), preset="1080x1080", mode="crop", crop_x=0.0, crop_y=0.0, crop_w=1.0, crop_h=0.5,
               lock_aspect=False)
    assert (free.src_w, free.src_h) == (1920, 540) and free.dst_w == 1080 and free.dst_h == 304
    with pytest.raises(LayoutError):
        lay((1920, 1080), mode="crop", crop_x=0.8, crop_w=0.5)


def test_custom_position_and_zoom_clips_to_the_canvas():
    layout = lay((1920, 1080), preset="1920x1080", mode="custom", zoom=2.0, pos_x=0.5, pos_y=0.5)
    assert (layout.src_x, layout.src_y, layout.src_w, layout.src_h) == (480, 270, 960, 540)
    assert layout.background == "none"
    corner = lay((1920, 1080), preset="1920x1080", mode="custom", zoom=0.5, pos_x=0.25, pos_y=0.25)
    assert (corner.dst_x, corner.dst_y, corner.dst_w, corner.dst_h) == (0, 0, 960, 540)
    off = lay((1920, 1080), preset="1920x1080", mode="custom", zoom=1.0, pos_x=0.0, pos_y=0.5)
    assert off.src_x == 960 and off.dst_w == 960  # left half of the canvas shows the right half of the video
    corner = lay((1920, 1080), preset="1920x1080", mode="custom", zoom=0.2, pos_x=1.0, pos_y=1.0)
    # The centre is always on the canvas, so at least a quarter of the video stays visible.
    assert (corner.src_x, corner.src_y, corner.src_w) == (0, 0, 960) and corner.dst_x + corner.dst_w == 1920


def test_custom_size_validation():
    assert lay((1280, 720), preset="custom", width=1000, height=500).out_h == 500
    for w, h in ((999, 500), (32, 500), (8000, 500), (1000, 5000)):
        with pytest.raises(LayoutError):
            lay((1280, 720), preset="custom", width=w, height=h)
    for bad in (dict(background="red"), dict(blur=0), dict(zoom=5), dict(pos_x=2), dict(mode="stretch")):
        with pytest.raises(LayoutError):
            ResizeSettings(**bad).validate()


def test_odd_source_sizes_stay_inside_the_frame():
    layout = lay((641, 361), preset="original", mode="fit")
    assert (layout.out_w, layout.out_h) == (640, 360) and layout.src_w == 640 and layout.src_h == 360


def test_filters_for_each_background():
    pad = layout_filter(lay((1920, 1080), preset="1080x1920", mode="fit", background="#112233"), "a", "b")
    assert pad == ("[a]crop=1920:1080:0:0,scale=1080:608:flags=lanczos,setsar=1,"
                   "pad=1080:1920:0:656:color=0x112233[b]")
    blur = layout_filter(lay((1920, 1080), preset="1080x1920", mode="blur", blur=30), "a", "b")
    assert "split=2[lfg][lbg]" in blur and "overlay=x=0:y=656[b]" in blur
    # blurred at a quarter of the output size (radius 30 → 8), then scaled back up to the canvas
    assert "scale=270:480:force_original_aspect_ratio=increase" in blur and "crop=270:480" in blur
    assert "boxblur=luma_radius=8:luma_power=2,scale=1080:1920:flags=bilinear" in blur
    tiny = layout_filter(lay((1920, 1080), preset="custom", width=64, height=96, mode="blur", blur=60), "a", "b")
    assert "boxblur=luma_radius=15" in tiny  # limited by the frame size
    fill = frame_filter(lay((1920, 1080), preset="1080x1920", mode="fill"))
    assert fill == "[0:v:0]crop=608:1080:656:0,scale=1080:1920:flags=lanczos,setsar=1[vout]"


def test_round_trip():
    s = ResizeSettings(preset="custom", width=1000, height=600, mode="crop", burn_captions=True)
    assert ResizeSettings.from_dict(s.to_dict() | {"unknown": 1}) == s
    layout = lay((1920, 1080))
    assert type(layout).from_dict(layout.to_dict()) == layout
