FUNCTION onclick_edit_kyc
    LOGIC
        if: System.If(condition = Url.pathParts[3] != undefined)
            true
                navigate: UIEngine.Navigate(linkPath = `'/kyc/{{Store.auth.loggedInClientCode}}/page/kycIndividual/{{Page.kycDetails._id}}/{{Page.kycDetails.userId}}/{{Url.pathParts[3]}}?redirect=\\/customerProfile/kycdetails/{{Url.pathParts[2]}}/{{Url.pathParts[3]}}'`) AFTER Steps.if.true
            false
                navigate1: UIEngine.Navigate(linkPath = `'/kyc/{{Store.auth.loggedInClientCode}}/page/kycIndividual/{{Page.kycDetails._id}}/{{Page.kycDetails.userId}}?redirect=\\/customerProfile/kycdetails/{{Url.pathParts[2]}}'`) AFTER Steps.if.false