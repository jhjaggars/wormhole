FROM centos:centos7
RUN yum install -y python-devel && yum clean all
RUN curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py && python get-pip.py && rm get-pip.py 
COPY wormhole/* wormhole/
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt
CMD ["python", "wormhole/__init__.py"] 
