import asyncio
from pathlib import Path

import cv2
import httpx
import numpy as np

from sv_face_verifier.server import create_app, MAX_BYTES


KEY = 'test-only-shared-verifier-secret-123456'


def test_real_models_encode_a_single_face_and_reject_invalid_requests():
    app = create_app(key=KEY)

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://verifier') as client:
                path = Path(__file__).parents[2] / 'frontend/face_attendance/assets/samples/live.jpg'
                headers = {'X-Face-Verifier-Key': KEY}
                assert (await client.post('/v1/faces/encode', content=path.read_bytes())).status_code == 401
                response = await client.post('/v1/faces/encode', content=path.read_bytes(), headers=headers)
                assert response.status_code == 200
                assert response.json()['model'] == 'opencv-sface-2021dec-v1'
                vector = response.json()['vectors']
                assert len(vector) == 1 and len(vector[0]) == 128
                assert abs(np.linalg.norm(vector[0]) - 1) < 0.001
                blank = cv2.imencode('.jpg', np.zeros((100, 100, 3), dtype=np.uint8))[1].tobytes()
                assert (await client.post('/v1/faces/encode', content=blank, headers=headers)).json()['vectors'] == []
                assert (await client.post('/v1/faces/encode', content=b'broken', headers=headers)).status_code == 422
                assert (await client.post('/v1/faces/encode', content=b'x' * (MAX_BYTES + 1), headers=headers)).status_code == 413

    asyncio.run(check())
