#!/bin/bash

user=$(whoami)
dump='/home/'$user'/Pictures/homeSec'
zips='/home/'$user'/Pictures/sendFiles'
date=$(date +"%Y-%m-%d_%H%M")
email= <! emal address !>
mkdir -p $dump
mkdir -p $zips

sleep 2m

echo '-------------------------------------'
echo '|Zipping files and removing orf File|'
echo '-------------------------------------'
cd $dump
zip -r $zips/homeSec_$date.zip *
rm $dump/*

echo '--------------------------------------------------------'
echo '|sending' $zips/homeSec_$date.zip to $email
echo '---------------------------------------------------------'

echo "neuze neuze" | mutt -s "Motion Detected" -a $zips/homeSec_$date.zip -- $emaily

