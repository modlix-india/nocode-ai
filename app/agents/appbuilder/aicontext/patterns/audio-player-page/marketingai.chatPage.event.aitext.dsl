FUNCTION aitext
    LOGIC
        postRequest: CoreServices.REST.PostRequest(connectionName = "TESTCHATAI", payload = Page.speech, url = "/api/ds/audio/transcribe")
            output
                setStore: UIEngine.SetStore(path = "Page.draft", value = Steps.postRequest.output.data.text)