from gpiozero import LED
from time import sleep

# GPIO27 controls INHIBIT+
inhibit = LED(27)

print("Starting inhibit test...")

while True:
    print("\n--- TEST: GPIO LOW ---")
    print("Setting INHIBIT = LOW")
    inhibit.off()   # GPIO LOW (0V)

    print("Try inserting bill now...")
    sleep(10)

    print("\n--- TEST: GPIO HIGH ---")
    print("Setting INHIBIT = HIGH")
    inhibit.on()    # GPIO HIGH (3.3V)

    print("Try inserting bill now...")
    sleep(10)