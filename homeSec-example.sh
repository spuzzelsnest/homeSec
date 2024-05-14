#!/bin/bash

user=$(whoami)
dump='motion/'
zips='sendFiles/'
date=$(date +"%Y-%m-%d_%H%M")
email= <! email address !>
mkdir -p $dump
mkdir -p $zips


echo '----------------'
echo '|Taking picture|'
echo '----------------'
fswebcam -r 1280x720 --no-banner $dump/$date.jpg

sleep 1m

echo '-------------------------------------'
echo '|Zipping files and removing org File|'
echo '-------------------------------------'
cd $dump
zip -r $zips/homeSec_$date.zip *
rm $dump/*

echo '---------------------------------------------------------'
echo '|sending' $zips/homeSec_$date.zip to $email
echo '---------------------------------------------------------'

echo "neuze neuze" | mutt -s "Motion Detected" -a $zips/homeSec_$date.zip -- $email
