FUNCTION viewDetailsNavigate
    LOGIC
        navigate: UIEngine.Navigate(target = "_self", linkPath = `'/projectLandingPage/{{Page.projects[Parent.__index].projectFullName}}'`)