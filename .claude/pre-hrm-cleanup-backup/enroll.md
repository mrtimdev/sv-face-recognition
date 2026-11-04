

python enroll_faces.py --name "Tim Dev" --image faces/tim-dev.png
python enroll_faces.py --name "Mr A" --image faces/mr-a.jpg
python enroll_faces.py --name "Mr B" --image faces/mr-b.jpg
python enroll_faces.py --name "Mrs A" --image faces/mrs-a.jpg



# Activate virtual environment
source venv/bin/activate

# Run recognition with enhanced UI
python recognize.py

# Optional: with custom RTSP source or tighter tolerance
python recognize.py --tolerance 0.45 --process-every 2