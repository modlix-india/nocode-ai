FUNCTION fetchEoi
    LOGIC
        fud: ZohoFunctions.FetchUploadedDocument(emailId = "<EMAIL>", kycId = "roops", projectId = "ffhhh4646")
            output
                print: System.Print(values = Steps.fud.output.result)