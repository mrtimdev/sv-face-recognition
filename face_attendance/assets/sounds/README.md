These fourteen short notification WAVs are original, synthesized project assets.
No downloaded recordings or speech services are used. Regenerate with
`python scripts/generate_builtin_sounds.py` (Python standard library only).
Files are mono, 16-bit PCM, 22050 Hz, with smooth attack/release and modest gain.

The inactive employee cue is a separate low three-note warning.
Each event has a distinct melody: detected, lost, unknown, recognized, multiple,
spoof (presentation rejected), liveness guidance, quality guidance, verified,
success, error, disconnected and reconnected. Recognition alone does not mean
liveness passed or attendance was saved. A rejected presentation cue indicates
the model check failed, not proof of an attack.
