FROM python:3.8-slim-bullseye

LABEL maintainer "DataMade <info@datamade.us>"

RUN apt update && apt install -y gcc g++

RUN mkdir /app
WORKDIR /app

# Copy the contents of the current host directory (i.e., our app code) into
# the container.
COPY . /app

RUN pip install -e .

RUN pip install -r requirements-dev.txt
RUN pip install -r requirements-examples.txt

