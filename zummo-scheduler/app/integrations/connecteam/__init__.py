"""Factory: hands the app whichever ConnecTeam adapter is appropriate.

Picks the stub unless real credentials are configured. When you build the
real adapter, add the import + branch here — nothing else in the app needs
to know which implementation is live.
"""

from app.config import settings
from app.integrations.connecteam.base import ConnecTeamAdapter
from app.integrations.connecteam.stub import StubConnecTeam


def get_connecteam() -> ConnecTeamAdapter:
    if settings.use_connecteam:
        # TODO(real): return RealConnecTeam(settings.connecteam_api_key,
        #                                   settings.connecteam_base_url)
        raise NotImplementedError(
            "ConnecTeam credentials are set but the real adapter isn't built "
            "yet. Implement RealConnecTeam and wire it in here."
        )
    return StubConnecTeam()
