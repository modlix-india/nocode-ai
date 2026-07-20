FUNCTION inviteSuccess
    LOGIC
        successInvite: UIEngine.SetStore(path = "Page.inviteSent", value = not Page.inviteSent)
        makingInvisible: UIEngine.SetStore(path = "Page.inviteUser", value = false)