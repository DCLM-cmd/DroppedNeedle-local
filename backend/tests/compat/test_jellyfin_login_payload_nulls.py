"""No boolean in the login payload may reach a client as null.

Strict clients hard-cast these fields. Finamp fails the whole login with
``type 'Null' is not a subtype of 'bool' in type cast`` when one is missing, and it
cannot be recovered from in the app - the user simply cannot sign in.

This has now bitten twice: issue #144 typed UserConfiguration and SessionInfo for
exactly this reason, but Policy stayed a hand-written dict and was missing nine of
Jellyfin's policy booleans. This test guards the whole payload rather than the field
that happened to be missing, so the next omission fails here instead of on a phone.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import msgspec
import pytest

import api.compat.jellyfin.models as jm
from api.compat.jellyfin.router import _user_dto

# Genuinely nullable in Jellyfin's own schema, and nullable in the client models too.
_NULLABLE = {
    ".User.Configuration.AudioLanguagePreference",
    ".User.Configuration.SubtitleLanguagePreference",
    ".User.Policy.MaxParentalRating",
}


def _payload(role: str = "admin") -> dict:
    user = SimpleNamespace(
        id="u1", username="nils", username_display="Nils",
        display_name="Nils", role=role,
    )
    session = jm.SessionInfo(
        Id="s1", UserId="u1", UserName="Nils",
        LastActivityDate=datetime.now(timezone.utc).isoformat(),
        Client="Finamp", DeviceName="Phone", DeviceId="d1",
    )
    return json.loads(
        msgspec.json.encode(
            jm.AuthenticationResult(
                User=_user_dto(user), AccessToken="pw", SessionInfo=session
            )
        )
    )


def _nulls(value, path: str = "") -> list[str]:
    if isinstance(value, dict):
        return [found for key, item in value.items() for found in _nulls(item, f"{path}.{key}")]
    return [] if value is not None else [path]


@pytest.mark.parametrize("role", ["admin", "user"])
def test_the_login_payload_carries_no_unexpected_nulls(role) -> None:
    assert set(_nulls(_payload(role))) <= _NULLABLE


def test_every_policy_boolean_is_populated() -> None:
    policy = _payload()["User"]["Policy"]
    assert not [key for key, value in policy.items() if value is None and key != "MaxParentalRating"]


@pytest.mark.parametrize(
    "field",
    [
        # The nine that were missing and produced the crash.
        "EnableCollectionManagement",
        "EnableContentDeletion",
        "EnableLiveTvManagement",
        "EnableLyricManagement",
        "EnableMediaConversion",
        "EnablePublicSharing",
        "EnableSubtitleDownloading",
        "EnableSubtitleManagement",
        "ForceRemoteSourceTranscoding",
        # ...and the ones that were already there, so they cannot be dropped either.
        "IsAdministrator",
        "IsDisabled",
        "IsHidden",
        "EnableAllFolders",
        "EnableMediaPlayback",
        "EnableRemoteAccess",
    ],
)
def test_jellyfins_policy_booleans_are_all_present(field) -> None:
    policy = _payload()["User"]["Policy"]
    assert isinstance(policy.get(field), bool), f"{field} must be a real bool, not null"


def test_administrator_still_reflects_the_role() -> None:
    """Typing the policy must not flatten the one value that is actually derived."""
    assert _payload("admin")["User"]["Policy"]["IsAdministrator"] is True
    assert _payload("user")["User"]["Policy"]["IsAdministrator"] is False


def test_session_and_configuration_booleans_stay_populated() -> None:
    """The #144 fields: they must not regress while Policy is being changed."""
    payload = _payload()
    for block in (payload["SessionInfo"], payload["User"]["Configuration"]):
        assert not [key for key, value in block.items() if value is None and not key.endswith("Preference")]
