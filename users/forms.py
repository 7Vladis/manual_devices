from django import forms
from django.contrib.auth.forms import AuthenticationForm

class LoginForm(AuthenticationForm):
    remember_me = forms.BooleanField(required=False, initial=True, label="Запомнить меня")
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Добавляем классы Bootstrap для стилизации
        self.fields['username'].widget.attrs.update({
            'class': 'form-control', 'placeholder': 'Email address'
        })
        self.fields['password'].widget.attrs.update({
            'class': 'form-control', 'placeholder': 'Password'
        })
        self.fields['remember_me'].widget.attrs.update({
            'class': 'form-check-input'
        })