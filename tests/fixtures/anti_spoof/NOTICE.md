# MiniFASNet reference fixtures

Unmodified example images from [yakhyo/face-anti-spoofing](https://github.com/yakhyo/face-anti-spoofing/tree/main/assets), downloaded 2026-09-24. Upstream project is Apache-2.0; its license is included. These are public upstream examples, not employee images.

| Local name | Upstream name | SHA-256 |
| --- | --- | --- |
| live.jpg | image_T1.jpg | f4455149f488f76205fdee5499ec5261d08ef6279a1cff7b778ea85405331e94 |
| print.jpg | image_F1.jpg | 4b11b5d7a8a8e4a88f5f16a5426a0a7692e39e5bb45bb03b4ebe5e1606336860 |
| screen.jpg | image_F2.jpg | fbbea73450ae9d9bb555c8ccac77bf39d234261fe3be4190e3ed2999690c485f |

The live/printed-photo/screen labels follow upstream's examples. Tests verify the real bundled models, preprocessing and detector on these three samples. They do not establish deployment accuracy, video replay protection or mask detection. Never treat repeated copies of the live example as a physical presentation-attack test.
