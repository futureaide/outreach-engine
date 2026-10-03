from execution import source_leads as source_module


def _lead(idx: int, *, title: str, industry: str = "marketing & advertising", size: str = "11-50") -> dict:
    return {
        "email": f"lead{idx}@example.com",
        "first_name": "Pat",
        "last_name": "Test",
        "company_name": "Example Co",
        "title": title,
        "linkedin_url": "",
        "company_website": "https://example.com",
        "email_status": "validated",
        "company_size": size,
        "company_industry": industry,
    }


def test_icp_pretest_returns_failure_snapshots():
    icp = {
        "titles": ["Founder", "CEO", "Owner"],
        "industries": ["marketing & advertising"],
        "company_size_ranges": ["11-50", "51-200"],
    }
    leads = [
        _lead(1, title="Account Manager", industry="public relations & communications"),
        _lead(2, title="Creative Director", industry="public relations & communications"),
        _lead(3, title="Founder", industry="marketing & advertising", size="11-50"),
    ]

    passed, pass_rate, analysis = source_module.icp_pretest(leads, icp=icp)

    assert passed is False
    assert round(pass_rate, 2) == 0.33
    assert analysis["dominant_failure_reason"] in {"title_mismatch", "industry_mismatch", "exclusion_hit"}
    assert analysis["mismatched_titles_sample"]
    assert analysis["failed_industries_sample"]


def test_company_size_exact_count_matches_allowed_range():
    icp = {
        "titles": ["Founder", "CEO", "Owner"],
        "industries": ["marketing & advertising"],
        "company_size_ranges": ["11-50", "51-200"],
    }

    assert source_module._company_size_excluded("39", icp) is False
    assert source_module._company_size_excluded("56", icp) is False
    assert source_module._company_size_excluded("2", icp) is True


def test_icp_pretest_accepts_numeric_company_sizes():
    icp = {
        "titles": ["Founder", "CEO", "Owner"],
        "industries": ["marketing & advertising"],
        "company_size_ranges": ["11-50", "51-200"],
    }
    leads = [
        _lead(1, title="Founder", size="39"),
        _lead(2, title="CEO", size="56"),
        _lead(3, title="Owner", size="2"),
    ]

    passed, pass_rate, analysis = source_module.icp_pretest(leads, icp=icp)

    assert passed is False
    assert round(pass_rate, 2) == 0.67
    assert analysis["bad_company_size"] == 1


def test_source_from_apify_switches_to_next_profile_after_failed_pretest(monkeypatch):
    icp = {
        "titles": ["Founder", "CEO", "Owner", "Head of Growth"],
        "industries": ["marketing & advertising"],
        "company_size_ranges": ["11-50", "51-200"],
        "query_rotation": [
            {
                "contact_job_title": ["Founder", "CEO", "Owner"],
                "company_industry": ["marketing & advertising"],
                "seniority_level": ["founder", "owner", "c_suite"],
                "organization_num_employees_ranges": ["11-50", "51-200"],
            }
        ],
    }

    class DummyClient:
        def __init__(self, _token):
            self.token = _token

    monkeypatch.setattr(source_module, "ApifyClient", DummyClient)
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")

    state = {"good_calls": 0}

    def fake_fetch(run_input, _client):
        titles = run_input.get("contact_job_title", [])
        fetch_count = run_input.get("fetch_count", 100)

        if "Founder" in titles:
            base = state["good_calls"] * 100
            state["good_calls"] += 1
            return [_lead(base + i, title="Founder") for i in range(fetch_count)]

        return [_lead(i, title="Creative Strategist") for i in range(fetch_count)]

    monkeypatch.setattr(source_module, "_fetch_from_apify", fake_fetch)

    leads, pretest_info = source_module.source_from_apify(
        query="head of growth performance marketing agency",
        location="United States",
        count=150,
        icp=icp,
    )

    assert len(leads) == 150
    assert pretest_info["attempts"] == 3
    assert pretest_info["winning_profile"] == "query-rotation-1"
    assert pretest_info["winning_params"]["contact_job_title"] == ["Founder", "CEO", "Owner"]
