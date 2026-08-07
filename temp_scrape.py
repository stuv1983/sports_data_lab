import urllib.request, re

req = urllib.request.Request('https://gridleygame.com/', headers={'User-Agent': 'Mozilla/5.0'})
html = urllib.request.urlopen(req).read().decode('utf-8')
chunks = re.findall(r'src="(.*?\.js)"', html)
urls = [c if c.startswith('http') else 'https://gridleygame.com' + c for c in chunks]

for u in urls:
    if 'adsbygoogle' in u: continue
    try:
        req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
        js = urllib.request.urlopen(req).read().decode('utf-8')
        
        fetches = re.findall(r'fetch\([\'"](.*?)[\'"]\)', js)
        if fetches:
            print(f"Fetches in {u}: {fetches}")
            
        gets = re.findall(r'\.get\([\'"](.*?)[\'"]\)', js)
        if gets:
            print(f"Axios/Gets in {u}: {gets}")
    except Exception as e:
        pass
