FUNCTION basicDetails
    LOGIC
        navigate: UIEngine.Navigate(linkPath = `'/basicDetails/{{Url.pathParts[1]}}'`)