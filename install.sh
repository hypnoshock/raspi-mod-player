#!/bin/bash

apt update
apt install xmp screen
cp -f .bashrc ~/.bashrc
cp -f config.txt /boot/firmware
cp -f gpio_keypress.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable gpio_keypress
systemctl start gpio_keypress
