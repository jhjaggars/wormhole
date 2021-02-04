FROM fedora:latest
COPY wormhole/* wormhole/
COPY requirements.txt requirements.txt
RUN ["python3", "-m", "ensurepip"]
RUN ["python3", "-m", "pip", "install", "-r", "requirements.txt"]
CMD ["python3", "wormhole/server.py"] 
