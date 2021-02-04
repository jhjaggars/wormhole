FROM fedora:latest
COPY wormhole/* wormhole/
COPY Pipfile.lock Pipfile.lock
RUN dnf -y install pipenv
RUN pipenv install
CMD ["pipenv", "run", "python" "wormhole/server.py"] 
