#!/bin/bash

dump='~/Pictures/homeSec'
zips='~/Pictures/sendFiles'
date=$(date +"%Y-%m-%d_%H%M")
email="j.mpdesmet@protonmail.com"
mkdir -p $dump
mkdir -p $zips

echo '----------------'
echo '|Taking picture|'
echo '----------------'

fswebcam -r 1280x720 --no-banner $dump/$date.jpg

sleep 1m

echo '-------------------------------------'
echo '|Zipping files and removing orf File|'
echo '-------------------------------------'
cd $dump
zip -r $zips/homeSec_$date.zip *
rm $dump/*

echo '--------------------------------------------------------'
echo '|sending' $zips/homeSec_$date.zip to $email
echo '---------------------------------------------------------'

echo "neuze neuze" | mail -s "Motion Detected" $email -A $zips/homeSec_$date.zip
