from evdev import UInput, ecodes as e
import time
from gpiozero import Button

# Define the input pin
next_button = Button(26, bounce_time=0.1)
back_button = Button(6, bounce_time=0.1)

ui = UInput()

def on_next_pressed():
    #print("press")
    ui.write(e.EV_KEY, e.KEY_UP, 1)
    ui.write(e.EV_KEY, e.KEY_UP, 0)  # Release key
    ui.syn()

def on_back_pressed():
    #print("press")
    ui.write(e.EV_KEY, e.KEY_DOWN, 1)
    ui.write(e.EV_KEY, e.KEY_DOWN, 0)  # Release key
    ui.syn()


def on_next_released():
    #print("release")
    pass

next_button.when_pressed = on_next_pressed
back_button.when_pressed = on_back_pressed

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    ui.close()

