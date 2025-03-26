#!/bin/bash

apt update
apt install xmp screen
cp -f .bashrc ~/.bashrc
cp -f config.txt /boot/firmware
