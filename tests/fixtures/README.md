# Test fixtures

Synthetic speech for the real-Whisper integration tests (`tests/integration/test_whisper_real.py`).
They were generated with espeak-ng, so no real person's voice or personal data is included:

```bash
espeak-ng -v en-us -s 140 -w en.wav "Hello and welcome to the video studio. Today we are testing automatic captions."
espeak-ng -v bn -s 130 -w bn.wav "আমি বাংলায় কথা বলি। আজ আমরা ক্যাপশন পরীক্ষা করছি।"
ffmpeg -i en.wav -ac 1 -ar 16000 -c:a flac speech_en.flac
ffmpeg -i bn.wav -ac 1 -ar 16000 -c:a flac speech_bn.flac
```

Robotic synthetic speech is harder to recognise than real recordings, so these files test that
the pipeline works. They are not a measure of accuracy.
