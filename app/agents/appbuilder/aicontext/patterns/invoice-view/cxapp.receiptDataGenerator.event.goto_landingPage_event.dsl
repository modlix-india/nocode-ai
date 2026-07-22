FUNCTION goto_landingPage_event
    LOGIC
        navigate: UIEngine.Navigate(linkPath = `'/projectLandingPage/{{Store.urlDetails.pathParts[1]}}'`)