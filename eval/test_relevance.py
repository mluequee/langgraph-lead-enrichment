"""Offline test of the relevance gate, built from real strings in the Phase 10 peek."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import first_graph as fg

LI = "https://linkedin.com/in/"
CASES = [
    ("Ballantyne Barbershop", "Ballantyne Barbershop, Inc | BBB Business Profile | Better Business Bureau", "https://www.bbb.org/us/nc/charlotte/profile/barber/ballantyne-barbershop-inc-0473-92031367", True),
    ("Ballantyne Barbershop", "Cordrick Landry - Owner at Landry's Barber Shoppe | LinkedIn", LI + "cordrick-landry-5322b89a/", False),
    ("Crown Chakra Barbering Co.", "Crown Chakra Barbering Company, 650 E. Stonewall St, Cha", "https://www.beautynailhairsalons.com/US/Charlotte/911408695557134/Crown-Chakra-Barbering-C", True),
    ("Crown Chakra Barbering Co.", "Scooter Clowney - Business Owner at Crown Barber Shop | L", LI + "scooter-clowney-b2522a46/", False),
    ("The Elects Barbershop/ book.thecut.co/theelects", "The Elects Barbershop 1810 Galleria Blvd suite 209", "https://www.instagram.com/p/B20PsXBgQSW/", True),
    ("The Elects Barbershop/ book.thecut.co/theelects", "Glen Vines - Owner at Executive Cuts barbershop | LinkedIn", LI + "glen-vines-b5847376/", False),
    ("Gordons' Historic Barbershop", "Gordon's Barbershop | Charlotte NC", "https://example.com/x", True),
    ("Distinguished Gentleman Barbershop", "Distingguished Gentleman Barbershop | Charlotte NC | Face", "https://www.facebook.com/DistingguishedGentlemanBarbershop/", True),
    ("Distinguished Gentleman Barbershop", "Scott Gordon - Owner - Barber Direct of Charlotte | Link", LI + "scott-gordon-3305038/", False),
    ("The Barber Lounge", "The Barber Lounge - Specialist in Beauty Services at 411", "https://thebeautyloc.com/branch/the-barber-lounge-lake-wylie-4110-c", True),
    ("The Barber Lounge", "Tino Leon - Barbershop Owner - Silver Lake Barber Shop | LinkedIn", LI + "tino-leon-61458b222/", False),
    ("Roosters Men's Grooming Center - Ballantyne", "Roosters MGC Ballantyne (@roostersmgcballantyne)", "https//:www.instagram.com/roostersmgcballantyne/?hl=en", True),
    ("who's Next Barber Shop", "Victoria Voncannon - Owner & Master Stylist at Major Bar", LI + "victoria-voncannon-7069b9ba/", False),
    ("Who's Next Barber Shop", "J Valentin - Barber/manager/teacher/owner at Who's next", LI + "j-valentin-0b618847/", True),
    ("Legendary Cuts @ University", "Legendary Cuts @ University | Charlotte", "https://example.com/1", True),
    ("Wilmar Barber Shop", "Greg Furr - Owner/Barber at Wil-Mar Barber Shop and Mt. ...", "https://www.linkedin.com/in/greg-furr-a751aa52/", True),
    ("Wilmar Barber Shop", "Wil-Mar Barber Shop - Concord, NC", "https://www.yelp.com/biz/wil-mar-barber-shop-concord", True),
    ("Wilmar Barber Shop", "Wil-Mar Service Center, Inc | Better Business Bureau", "https://www.bbb.org/us/nc/concord/profile/auto-repair/wil-mar-service-center-inc", False),
    ("The Elects Barbershop/ book.thecut.co/theelects", "Maurice Hunt (@theelectsbarbershop)", "https://www.instagram.com/theelectsbarbershop/", True),
    ("Roosters Men's Grooming Center - Ballantyne", "Rooster Property Care | BBB Business Profile | Better Bu", "https://www.bbb.org/us/nc/charlotte/profile/pressure-washing/rooster-property-care-0473-92", False),
]
bad = 0
for name, title, url, expected in CASES:
    got = fg._result_is_about_shop(title, "", url, name)
    bad += got != expected
    print("PASS" if got == expected else "FAIL", "|", name[:26].ljust(26), "|", title[:48].ljust(48), "->", got)
print(f"\n{len(CASES) - bad}/{len(CASES)} passed")

BOOKSY = [
    ("https://booksy.com/en-us/28874_the_barber-lounge_barber-shop_14977_colorado-springs", "Lake Wylie", True),
    ("https://booksy.com/en-us/185511_the-elects-barbershop_barber-shop_26844_charlotte", "Charlotte", False),
    ("https://booksy.com/en-us/1254405_landry-s-barber-shoppe_barber-shop_26689_gastonia#ba_s=seo", "Gastonia", False),
    ("https://booksy.com/en-us/1254405_landry-s-barber-shoppe_barber-shop_26689_gastonia#ba_s=seo", "Charlotte", True),
    ("https://booksy.com/en-us/1771460_distinguished-gentleman-barbershop-llc_barber-shop_121625", "Charlotte", False), #known leak: no city in URL
    ("https;//www.yelp.com/biz/roosters-mens-grooming-center-charlotte-5", "Charlotte", False),
]
bad2 = 0
for url, city, expected in BOOKSY:
    got = fg._booksy_url_names_other_place(url, city)
    bad2 += got != expected
    print("PASS" if got == expected else "FAIL", "| booksy |", url[-45:], "|", city, "->", got)
print(f"{len(BOOKSY) - bad2}/{len(BOOKSY)} booksy passed")