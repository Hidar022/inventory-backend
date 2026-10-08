from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts.serializers import (
    EmailTokenObtainPairSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PasswordResetTokenValidationSerializer,
    RegisterSerializer,
    UserSerializer,
)
from apps.organizations.permissions import OrganizationContextMixin

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class EmailTokenObtainPairView(TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer


class CurrentUserView(OrganizationContextMixin, generics.RetrieveAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class PasswordResetRequestView(generics.GenericAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetRequestSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "If an account exists for this email, we've sent a password reset link."},
            status=status.HTTP_200_OK,
        )


class PasswordResetValidationView(generics.GenericAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetTokenValidationSerializer

    def get(self, request, *args, **kwargs):
        token = request.query_params.get("token", "")
        serializer = self.get_serializer(data={"token": token})
        serializer.is_valid(raise_exception=True)
        reset_token = serializer.validated_data["reset_token"]
        return Response(
            {
                "valid": True,
                "email": reset_token.user.email,
                "expires_at": reset_token.expires_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class PasswordResetConfirmView(generics.GenericAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetConfirmSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = serializer.save()
        return Response(result, status=status.HTTP_200_OK)


__all__ = [
    "RegisterView",
    "EmailTokenObtainPairView",
    "TokenRefreshView",
    "CurrentUserView",
    "PasswordResetRequestView",
    "PasswordResetValidationView",
    "PasswordResetConfirmView",
]
