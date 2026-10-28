"""Shared official IR endpoint metadata; no settings, adapters or I/O on import."""
IR_FEEDS = {
    "US.MU": {
        "publisher": "Micron Technology Investor Relations",
        "url": "https://investors.micron.com/rss/news-releases.xml?items=30",
        "hosts": {"investors.micron.com"},
        "presentation_hub_url": "https://investors.micron.com/quarterly-results",
        "technology_scope": ["DRAM", "NAND"],
    },
    "US.SNDK": {
        "publisher": "Sandisk Corporation Investor Relations",
        "url": "https://investor.sandisk.com/rss/news-releases.xml",
        "hosts": {"investor.sandisk.com", "www.sandisk.com"},
        "presentation_hub_url": "https://investor.sandisk.com/news-events/presentations",
        "technology_scope": ["NAND"],
    },
    "US.WDC": {
        "publisher": "Western Digital Corporation Investor Relations",
        "url": "https://investor.wdc.com/rss/news-releases.xml",
        "hosts": {"investor.wdc.com", "www.westerndigital.com"},
        "presentation_hub_url": "https://investor.wdc.com/financial-information/earnings-documents",
        "technology_scope": ["HDD"],
    },
    "US.STX": {
        "publisher": "Seagate Technology Investor Relations",
        "url": "https://investors.seagate.com/rss/pressrelease.aspx",
        "hosts": {"investors.seagate.com"},
        "presentation_hub_url": "https://investors.seagate.com/financials/quarterly-results/default.aspx",
        "technology_scope": ["HDD"],
    },
}
