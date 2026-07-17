FUNCTION validations
    LOGIC
        eightCharactersMin: _.eightCharactersMin()
        oneUpperCaseCheck: _.oneUpperCaseCheck()
        oneLowerCaseCheck: _.oneLowerCaseCheck()
        oneDigitCheck: _.oneDigitCheck()
        oneSpecialCharacterCheck: _.oneSpecialCharacterCheck()
        passMatch: _.passMatch()
        if: System.If(condition = Page.specialCharacter and Page.oneDigit and  Page.oneLowerCase and Page.oneUpperCase and Page.eightCharacters and Page.passMatch)
            true
                setStore1: UIEngine.SetStore(path = "Page.allValidations", value = true) AFTER Steps.if.true
            false
                setStore: UIEngine.SetStore(path = "Page.allValidations", value = false) AFTER Steps.if.false