#!/bin/bash

user=$(logname)
groups=( mail video motion )
programs=( msmtp-mta mutt fswebcam zip mail motion mailutils)

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

mkdir /home/$user/homeSec/motion
chown $user:motion /home/$user/homeSec/motion
chmod 775 /home/$user/homeSec/motion

mkdir /var/log/motion
touch /var/log/motion/motion.log
chown motion:motion /var/log/motion/motion.log

echo done prepping
