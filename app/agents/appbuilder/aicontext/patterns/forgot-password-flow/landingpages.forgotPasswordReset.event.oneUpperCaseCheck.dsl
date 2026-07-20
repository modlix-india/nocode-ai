FUNCTION oneUpperCaseCheck
    LOGIC
        matches: System.String.Matches(string = `Page.reset.newPassword??''`, regex = ".*[A-Z]")
            output
                setStore: UIEngine.SetStore(path = "Page.oneUpperCase", value = Steps.matches.output.result)