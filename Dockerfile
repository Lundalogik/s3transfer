FROM docker.lundalogik.com/lundalogik/crm/python-base:latest

CMD sh

RUN apk --no-cache add git

# Set timezone to Sweden.
ENV TZ=Europe/Stockholm
RUN apk --no-cache add tzdata \
    && cp /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

WORKDIR /src
COPY . /src

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r requirements-test.txt
RUN pip install -e . --index-url=https://pypi.lundalogik.com:3443/lime/develop
