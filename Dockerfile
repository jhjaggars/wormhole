FROM fedora:latest
COPY wormhole/* wormhole/
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt
CMD ["python3", "wormhole/__init__.py"] 
