FUNCTION goto_landingPage_event
    LOGIC
        if: System.If(condition = `Url.pathParts[4] != 'RESIDENTIAL'`)
            true
                navigate: UIEngine.Navigate(linkPath = `'/landingPage/{{Url.pathParts[1]}}'`) AFTER Steps.if.true