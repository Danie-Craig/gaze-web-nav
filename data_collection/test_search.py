import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto('http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8082')

        # Inject debug listener that catches ALL events on the search bar
        await page.evaluate('''
            var searchBar = document.querySelector('input[name="q"]') || 
                            document.querySelector('input[type="search"]') || 
                            document.querySelector('#search');
            if (searchBar) {
                console.log("Found search bar: " + searchBar.outerHTML);
                ["input","change","keyup","keydown","keypress","focus","blur"].forEach(function(evt) {
                    searchBar.addEventListener(evt, function(e) {
                        console.log("EVENT: " + evt + " value: " + e.target.value);
                    });
                });
            } else {
                console.log("Search bar NOT found");
            }
        ''')

        # Listen to console logs
        page.on('console', lambda msg: print('BROWSER:', msg.text))

        print('Type in the search bar and press Enter. Watch for events.')
        await asyncio.sleep(30)
        await browser.close()

asyncio.run(test())