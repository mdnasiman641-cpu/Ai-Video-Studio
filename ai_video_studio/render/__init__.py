"""Rendering shared by Smart Cut, Resize & Crop and Bulk Processing (no Qt).

``output`` holds the user's encode settings, ``pipeline`` turns cuts, a video filter chain
and subtitles into one FFmpeg command, ``layout`` computes resize geometry (used by both the
preview and the export) and ``naming`` builds safe output file names.
"""
