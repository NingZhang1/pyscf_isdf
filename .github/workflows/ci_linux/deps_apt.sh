#!/usr/bin/env bash
sudo apt-get update
sudo apt-get -qq install \
    gcc \
    gfortran \
    libgfortran3 \
    libblas-dev \
    cmake \
    curl
