
CONFIDEX GUI setup

1. Recreate the virtual environment
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt

2. Restore the YOLO model weights
   Put your .pt file inside backend/ipModel/
   Expected default path:
   backend/ipModel/latesttrain.pt

3. Configure the web app base URL in .env.local
   Prefer either:
   BASE_URL=http://<YOUR_PC_LAN_IP>:3000
   or
   networkIP=<YOUR_PC_LAN_IP>:3000

4. Run the app
   python3 main.py

Notes
- The captures folder now stores sessions as:
  captures/[userID]/[timestamp]/raw.png
  captures/[userID]/[timestamp]/annotated.png
  captures/[userID]/[timestamp]/result_metadata.json
- If you test on a non-Raspberry Pi machine, the bill acceptor GPIO calls fall back to dummy classes.
