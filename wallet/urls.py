from django.urls import path

from . import views as v

urlpatterns = [
    path("health", v.HealthView.as_view()),
    path("auth/dev-token", v.DevTokenView.as_view()),
    path("wallets", v.WalletsView.as_view()),
    path("wallets/<str:pk>", v.WalletDetailView.as_view()),
    path("wallets/<str:pk>/topups", v.TopUpView.as_view()),
    path("wallets/<str:pk>/transactions", v.StatementView.as_view()),
    path("wallets/<str:pk>/withdrawals", v.WithdrawalsView.as_view()),
    path("transfers", v.TransfersView.as_view()),
    path("withdrawals/<str:pk>", v.WithdrawalDetailView.as_view()),
    path("bank/callbacks", v.BankCallbackView.as_view()),
    path("admin/wallets/<str:pk>/freeze", v.FreezeView.as_view()),
    path("admin/wallets/<str:pk>/unfreeze", v.UnfreezeView.as_view()),
    path("admin/wallets/<str:pk>/close", v.CloseView.as_view()),
    path("admin/wallets/<str:pk>/state-history", v.StateHistoryView.as_view()),
    path("admin/wallets/<str:pk>/daily-limit", v.DailyLimitView.as_view()),
    path("admin/transfers/<str:pk>/reverse", v.ReverseTransferView.as_view()),
    path("admin/flags", v.FlagsView.as_view()),
    path("admin/reconciliation", v.ReconciliationView.as_view()),
]
