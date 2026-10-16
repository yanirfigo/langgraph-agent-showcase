import pytest

from job_copilot.ingest.filters import is_excluded, role_of, wide_net


@pytest.mark.parametrize(
    ("title", "roles"),
    [
        ("GTM Engineer", ("gtm",)),
        ("Senior RevOps Manager", ("gtm",)),
        ("Go-to-Market Systems Lead", ("gtm",)),
        ("Business Applications Manager", ("bizapps",)),
        ("Salesforce Developer", ("bizapps",)),
        ("NetSuite Administrator", ("bizapps",)),
        ("CRM Architect", ("bizapps",)),
        ("AI Automation Lead", ("ai",)),
        ("Forward Deployed Engineer", ("ai",)),
        ("GenAI Solutions Manager", ("ai",)),
        ("AI Engineer, Salesforce Platform", ("bizapps", "ai")),
    ],
)
def test_target_roles(title, roles):
    assert role_of(title) == roles


@pytest.mark.parametrize(
    "title",
    [
        "Backend Engineer",
        "Product Manager",
        "Data Analyst",
        "AI Research Scientist",
        "ML Platform AI Engineer",
        "AI Engineer - Inference",
    ],
)
def test_non_targets(title):
    assert role_of(title) == ()


def test_ai_exclude_only_applies_to_ai_only_matches():
    # A bizapps match survives even when the title also has an AI-exclude word.
    assert role_of("Salesforce Platform AI Engineer") == ("bizapps", "ai")


@pytest.mark.parametrize(
    "title",
    [
        "Salesforce Intern",
        "Junior CRM Developer",
        "SDR Team Lead",
        "Product Marketing Manager",
        "סטודנט/ית Salesforce",
    ],
)
def test_excluded(title):
    assert is_excluded(title)


def test_wide_net():
    assert wide_net("Solutions Architect, Enterprise")
    assert wide_net("IT Business Applications Manager")
    assert not wide_net("ASIC Verification Engineer")
    assert not wide_net("Senior DevOps Engineer")
