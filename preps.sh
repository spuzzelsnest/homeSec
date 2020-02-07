#!/bin/bash

user=$(whoami)
groups=( mail video motion )
programs=( msmtp-mta fswebcam zip mail motion )

echo hi $user test, test
echo --------------------------
sudo apt-get update
for program in "${programs[@]}"; do
    if ! command -v "$program" > /dev/null 2>&1; then
        sudo apt-get install "$program" -y
    fi
done

echo Checking Groups
echo ---------------
for i in "${groups[@]}"; do
    if getent group $i  | grep &>/dev/null "\b${user}\b"; then
        echo Member of group $i
    else
        echo No Member of group $i
        sudo adduser $user $i
    fi
done

echo done prepping
