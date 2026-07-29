FUNCTION eightCharactersMin
    LOGIC
        matches: System.String.Matches(string = `Page.reset.newPassword??''`, regex = ".{8,}")
            output
                setStore: UIEngine.SetStore(path = "Page.eightCharacters", value = Steps.matches.output.result)