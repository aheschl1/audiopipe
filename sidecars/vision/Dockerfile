# Serving container for SigLIP 2 image/text embeddings (see app.py).
FROM python:3.11-slim

RUN pip install --no-cache-dir \
        "torch>=2.4" "torchvision>=0.19" \
        "transformers>=4.51" "sentencepiece>=0.2" "protobuf>=4" \
        "pillow>=10" "numpy" \
        "fastapi>=0.110" "uvicorn>=0.30" "python-multipart>=0.0.9"

WORKDIR /srv
COPY app.py /srv/app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
