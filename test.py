import requests

for i in range(1, 15):
    try:
        url = f"http://172.20.10.{i}/capture"
        r = requests.get(url, timeout=0.3)
        if r.status_code == 200:
            print("ESP32 FOUND:", url)
    except:
        pass
