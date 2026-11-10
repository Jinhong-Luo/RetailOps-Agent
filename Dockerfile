FROM python:3.12-slim
WORKDIR /app
COPY reports/requirements.resolved.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY vendor/tau2-bench/src/tau2 /app/vendor/tau2-bench/src/tau2
COPY vendor/tau2-bench/data/tau2/multilingual/zh/regional/retail.json /app/vendor/tau2-bench/data/tau2/multilingual/zh/regional/retail.json
COPY retailops /app/retailops
ENV PYTHONPATH=/app/vendor/tau2-bench/src RETAILOPS_DB=/data/retail.sqlite3
RUN mkdir /data && chown 65532:65532 /data
USER 65532:65532
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["python","-m","uvicorn","retailops.api:app","--host","0.0.0.0","--port","8000","--no-access-log"]

