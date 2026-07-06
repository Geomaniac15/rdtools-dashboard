import time
from adafruit_extended_bus import ExtendedI2C as I2C
import adafruit_sht4x

# Initialize the specific I2C bus (Bus 3 in your case)
# This replaces the default board.I2C() 
i2c = I2C(3)

# Connect to the SHT45 sensor
sht = adafruit_sht4x.SHT4x(i2c)

# Set the sensor to its highest precision mode
sht.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION

print("Starting SHT45 Data Stream on Bus 3... Press Ctrl+C to stop.")

try:
    while True:
        # Pull the data from the sensor
        temp, humidity = sht.measurements
        
        # Print it to the console beautifully
        print(f"Temperature: {temp:.2f} °C | Humidity: {humidity:.2f} %RH")
        
        # Wait 1 second before taking the next reading
        time.sleep(1)

except KeyboardInterrupt:
    print("\nData stream stopped.")