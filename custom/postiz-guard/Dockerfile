FROM python:3.12-alpine
RUN pip install --no-cache-dir "psycopg[binary]==3.2.*"
COPY guard.py /guard.py
VOLUME /data
CMD ["python", "-u", "/guard.py"]
