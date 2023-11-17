#! /bin/bash

conda init bash
source ~/.bashrc
cd /app
python rtc_otf.py -c config.yaml