FUNCTION goToPaymentEditDetailsPage
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.showDocs", value = "showBookings")
        if: System.If(condition = `Store.urlDetails.pathParts[1] = 'kycdetails'`)
            true
                setStore2: UIEngine.SetStore(path = "Page.showKycAccounts", value = "accounts") AFTER Steps.if.true