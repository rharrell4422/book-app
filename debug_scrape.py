from intelligence import fetch_page_text, generate_google_search_url

title = "The Path of Ascension 12: A LitRPG Adventure"
url = generate_google_search_url(title)

print("URL:", url)
print("\n--- PAGE TEXT START ---\n")
text = fetch_page_text(url)
print(text[:5000])  # print first 5000 chars
print("\n--- PAGE TEXT END ---")
