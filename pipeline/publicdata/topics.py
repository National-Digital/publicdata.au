"""The topics a dataset can be filed under.

A register entry names one or more, the home page offers them as the way in, and each has a page
listing what is served under it.
"""

TOPICS: dict[str, dict[str, str]] = {
    "roads": {
        "name": "Roads and transport",
        "blurb": "Crashes, casualties and deaths on the roads, by place, time and severity.",
        "search": "road crash",
    },
    "crime": {
        "name": "Crime and justice",
        "blurb": "Recorded offences and criminal incidents by area, offence and month.",
        "search": "crime",
    },
    "housing": {
        "name": "Housing and property",
        "blurb": "Rents, prices and land values by suburb and property type.",
        "search": "housing",
    },
    "migration": {
        "name": "Population and migration",
        "blurb": "Visa grants, settler arrivals and permanent additions, by stream and country.",
        "search": "migration",
    },
    "nature": {
        "name": "Nature and environment",
        "blurb": "Species records, water and coasts, from herbaria, atlases and monitoring stations.",
        "search": "environment",
    },
    "government": {
        "name": "Government and business",
        "blurb": "Who the agencies, businesses and charities are, from the registers that list them.",
        "search": "register",
    },
    "economy": {
        "name": "Prices and the economy",
        "blurb": "Fuel prices, interest rates and the other series that move week to week.",
        "search": "prices",
    },
    "places": {
        "name": "Places and boundaries",
        "blurb": "The boundaries of suburbs, postcodes, council areas and electorates that every located row is joined to.",
        "search": "boundaries",
    },
    "health": {
        "name": "Health and hospitals",
        "blurb": "Hospitals, emergency departments, notifiable diseases and the services that run them.",
        "search": "hospital",
    },
    "emergencies": {
        "name": "Fire, flood and emergencies",
        "blurb": "Bushfire and flood histories, incidents and the stations that answer them.",
        "search": "fire",
    },
    "people": {
        "name": "Births, deaths and names",
        "blurb": "Registered births, deaths and marriages, and the names parents choose.",
        "search": "births",
    },
    "community": {
        "name": "Community and services",
        "blurb": "Libraries, playgrounds, toilets, pets, holidays and the registers councils and agencies keep.",
        "search": "library",
    },
    "education": {
        "name": "Education",
        "blurb": "Schools, students and outcomes.",
        "search": "school",
    },
}
