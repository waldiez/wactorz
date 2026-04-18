ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache curl

RUN pip install --no-cache-dir "wactorz[all]"

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
