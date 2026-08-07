from playwright.sync_api import sync_playwright
import json

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        api_responses = {}

        def handle_response(response):
            if "json" in response.headers.get("content-type", "") and response.request.resource_type == "fetch":
                try:
                    data = response.json()
                    api_responses[response.url] = data
                except:
                    pass

        page.on("response", handle_response)
        
        print("Navigating to gridleygame.com...")
        page.goto("https://gridleygame.com/")
        page.wait_for_timeout(5000)
        
        for url, data in api_responses.items():
            print(f"URL: {url}")
            print(f"Data: {str(data)[:200]}...")
            
        # Or let's just scrape the HTML directly!
        html = page.content()
        # Find the categories visually!
        cats = page.query_selector_all("div.grid-cols-4 > div")
        print("Categories on page:")
        for c in cats:
            print(c.inner_text().replace('\n', ' '))
            
        browser.close()

if __name__ == "__main__":
    run()
