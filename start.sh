#!/bin/bash
# Download ML model if not present (too large for git)
MODEL_PATH="output/eye_disease_model.pt"
if [ ! -f "$MODEL_PATH" ]; then
    echo "Downloading ML model..."
    mkdir -p output
    # Replace MODEL_FILE_ID below with your Google Drive file ID
    FILE_ID="${MODEL_FILE_ID}"
    curl -L "https://drive.google.com/uc?export=download&id=${FILE_ID}&confirm=t" \
         -o "$MODEL_PATH"
    echo "Model downloaded."
fi

# Use the .venv interpreter — it has the full ML stack (torch, cv2, …) that the
# plain `venv` lacks, otherwise /xai/analyze fails with "ML dependencies not
# installed". Works on Windows (Scripts/) and Linux (bin/); falls back to the
# system python on hosts that install into the global env (e.g. cloud build).
if [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python"
fi

# Default to port 8000 for local runs where $PORT is unset (cloud sets $PORT).
"$PY" -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
