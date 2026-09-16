class ProfileProvisioningUnavailableError(Exception):
    """Registration cannot complete without confirmed profile provisioning."""


class ProfileProvisioningRejectedError(ProfileProvisioningUnavailableError):
    """Profile rejected a well-formed provisioning command permanently."""
