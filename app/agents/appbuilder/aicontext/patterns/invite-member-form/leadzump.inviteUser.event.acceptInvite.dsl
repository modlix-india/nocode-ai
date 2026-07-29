FUNCTION acceptInvite
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.invitedUserData.passType", value = `"PASSWORD"`)
            output
                sendData: UIEngine.SendData(url = "api/security/users/acceptInvite", method = "POST", payload = Page.invitedUserData) AFTER Steps.setStore.output
                    output
                        if: System.If(condition = Steps.sendData.output.data)
                            true
                                setStore1: UIEngine.SetStore(path = "Page.showStatusMessage", value = `true`) AFTER Steps.if.true
                                    output
                                        wait: System.Wait(millis = 2000) AFTER Steps.setStore1.output
                                            output
                                                navigate: UIEngine.Navigate(linkPath = "/deals", force = true) AFTER Steps.wait.output
                            false
                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.showStatusMessage", value = `false`) AFTER Steps.if.false
                            output
                                setStore2: UIEngine.SetStore(path = "Page.showStatus", value = `true`) AFTER Steps.if.output